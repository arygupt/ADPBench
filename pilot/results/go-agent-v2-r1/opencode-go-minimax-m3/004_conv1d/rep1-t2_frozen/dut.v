// 1-D convolution: y[f][o] = sum_k x[o+k] * w[f][k]
// F=4 filters, K=4 taps, IN_LEN=128, OUT_POS=125 -> 500 outputs per transaction
// 2 back-to-back transactions, no reset between
// Strategy: 4 parallel multipliers + 1 add tree, combinational output
// Use 4 interleaved x memories to reduce mux size

module dut #(
    parameter IN_LEN  = 128,
    parameter F       = 4,
    parameter K       = 4,
    parameter LANES   = 16,
    parameter DATA_W  = 8,
    parameter ACC_W   = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_w_flat,
    input  wire                    in_w_flat_valid,
    output wire                    in_w_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam [1:0] S_IDLE    = 2'd0,
                      S_COMPUTE = 2'd1;
    reg [1:0] state;

    // 4 interleaved x memories: x_memk[i] = x[4*i + k]
    (* ram_style = "distributed" *)
    reg signed [DATA_W-1:0] x_mem0 [0:IN_LEN/4-1];
    (* ram_style = "distributed" *)
    reg signed [DATA_W-1:0] x_mem1 [0:IN_LEN/4-1];
    (* ram_style = "distributed" *)
    reg signed [DATA_W-1:0] x_mem2 [0:IN_LEN/4-1];
    (* ram_style = "distributed" *)
    reg signed [DATA_W-1:0] x_mem3 [0:IN_LEN/4-1];

    (* ram_style = "distributed" *)
    reg signed [DATA_W-1:0] w_mem [0:F*K-1];

    reg [7:0] x_cnt;
    reg [4:0] w_cnt;
    reg [1:0] f_idx;
    reg [7:0] o_pos;
    reg out_v;

    // For x read: o_pos = 4*i + j, j = o_pos % 4
    wire [1:0] j = o_pos[1:0];
    wire [4:0] i = o_pos[6:2];

    wire [4:0] i1 = i + 5'd1;

    // Select the right memory and index for each of the 4 reads
    wire signed [DATA_W-1:0] x_val_0 =
        (j == 2'd0) ? x_mem0[i] :
        (j == 2'd1) ? x_mem1[i] :
        (j == 2'd2) ? x_mem2[i] :
                       x_mem3[i];

    wire signed [DATA_W-1:0] x_val_1 =
        (j == 2'd0) ? x_mem1[i] :
        (j == 2'd1) ? x_mem2[i] :
        (j == 2'd2) ? x_mem3[i] :
                       x_mem0[i1];

    wire signed [DATA_W-1:0] x_val_2 =
        (j == 2'd0) ? x_mem2[i] :
        (j == 2'd1) ? x_mem3[i] :
        (j == 2'd2) ? x_mem0[i1] :
                       x_mem1[i1];

    wire signed [DATA_W-1:0] x_val_3 =
        (j == 2'd0) ? x_mem3[i] :
        (j == 2'd1) ? x_mem0[i1] :
        (j == 2'd2) ? x_mem1[i1] :
                       x_mem2[i1];

    wire signed [2*DATA_W-1:0] p0_c = x_val_0 * w_mem[f_idx*K + 0];
    wire signed [2*DATA_W-1:0] p1_c = x_val_1 * w_mem[f_idx*K + 1];
    wire signed [2*DATA_W-1:0] p2_c = x_val_2 * w_mem[f_idx*K + 2];
    wire signed [2*DATA_W-1:0] p3_c = x_val_3 * w_mem[f_idx*K + 3];

    wire signed [ACC_W-1:0] sum_c =
        $signed({{16{p0_c[15]}}, p0_c}) +
        $signed({{16{p1_c[15]}}, p1_c}) +
        $signed({{16{p2_c[15]}}, p2_c}) +
        $signed({{16{p3_c[15]}}, p3_c});

    assign out_valid = out_v;
    assign out_c     = sum_c;

    assign in_x_flat_ready = (state == S_IDLE) && (x_cnt < IN_LEN);
    assign in_w_flat_ready = (state == S_IDLE) && (w_cnt < F*K);

    integer jl;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state     <= S_IDLE;
            x_cnt     <= 0;
            w_cnt     <= 0;
            f_idx     <= 0;
            o_pos     <= 0;
            out_v     <= 0;
        end else begin
            case (state)
                S_IDLE: begin
                    out_v <= 0;
                    if (in_x_flat_valid && in_x_flat_ready) begin
                        // Write LANES elements, distributing across 4 memories
                        // For lane jl, x index = x_cnt + jl
                        // memory index m = (x_cnt + jl) >> 2
                        // memory select k = (x_cnt + jl) & 3
                        for (jl = 0; jl < LANES; jl = jl + 1) begin
                            case ((x_cnt + jl[1:0]) & 2'd3)
                                2'd0: x_mem0[(x_cnt + jl) >> 2] <= $signed(in_x_flat[jl*DATA_W +: DATA_W]);
                                2'd1: x_mem1[(x_cnt + jl) >> 2] <= $signed(in_x_flat[jl*DATA_W +: DATA_W]);
                                2'd2: x_mem2[(x_cnt + jl) >> 2] <= $signed(in_x_flat[jl*DATA_W +: DATA_W]);
                                2'd3: x_mem3[(x_cnt + jl) >> 2] <= $signed(in_x_flat[jl*DATA_W +: DATA_W]);
                            endcase
                        end
                        x_cnt <= x_cnt + LANES;
                    end
                    if (in_w_flat_valid && in_w_flat_ready) begin
                        for (jl = 0; jl < LANES; jl = jl + 1) begin
                            if (w_cnt + jl < F*K)
                                w_mem[w_cnt + jl] <= $signed(in_w_flat[jl*DATA_W +: DATA_W]);
                        end
                        w_cnt <= w_cnt + LANES;
                    end
                    if (x_cnt >= IN_LEN && w_cnt >= F*K) begin
                        state <= S_COMPUTE;
                        f_idx <= 0;
                        o_pos <= 0;
                        out_v <= 1;
                    end
                end

                S_COMPUTE: begin
                    if (o_pos == IN_LEN - K && f_idx == F - 1) begin
                        out_v <= 0;
                        f_idx <= 0;
                        o_pos <= 0;
                        state <= S_IDLE;
                        x_cnt <= 0;
                        w_cnt <= 0;
                    end else begin
                        out_v <= 1;
                        if (o_pos == IN_LEN - K) begin
                            o_pos <= 0;
                            f_idx <= f_idx + 1;
                        end else begin
                            o_pos <= o_pos + 1;
                        end
                    end
                end
            endcase
        end
    end

endmodule