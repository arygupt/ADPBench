module dut #(
    parameter M = 8,
    parameter N = 8,
    parameter K = 16,
    parameter LANES = 16,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_b_flat,
    input  wire                    in_b_flat_valid,
    output wire                    in_b_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam A_SIZE = M*K;
    localparam B_SIZE = K*N;
    localparam A_BEATS = (M*K)/LANES;
    localparam B_BEATS = (K*N)/LANES;

    localparam S_RECV = 2'd0;
    localparam S_COMP = 2'd1;
    localparam S_OUT  = 2'd2;

    reg [1:0] state;

    // beat counters
    reg [$clog2(A_BEATS+1)-1:0] a_cnt;
    reg [$clog2(B_BEATS+1)-1:0] b_cnt;

    // compute indices
    reg [$clog2(M+1)-1:0] row_idx;
    reg [$clog2(K+1)-1:0] k_idx;
    reg [$clog2(N+1)-1:0] col_idx;

    // storage
    reg signed [DATA_W-1:0] a_mem [0:M*K-1];
    reg signed [DATA_W-1:0] b_mem [0:K*N-1];

    // accumulators, one per column
    reg signed [ACC_W-1:0] acc [0:N-1];

    // handshake helpers
    wire a_handshake = in_a_flat_valid & in_a_flat_ready;
    wire b_handshake = in_b_flat_valid & in_b_flat_ready;

    assign in_a_flat_ready = (state == S_RECV) && (a_cnt < A_BEATS);
    assign in_b_flat_ready = (state == S_RECV) && (b_cnt < B_BEATS);

    assign out_valid = (state == S_OUT);
    assign out_c = (state == S_OUT) ? acc[col_idx] : {ACC_W{1'b0}};

    // current A value and B row values for compute
    // indices: a = row*K + k, b = k*N + j
    wire [$clog2(M*K+1)-1:0] a_rd_idx;
    assign a_rd_idx = row_idx * K + k_idx;

    reg signed [DATA_W-1:0] a_val;
    reg signed [DATA_W-1:0] b_vals [0:N-1];

    integer rdi;
    always @* begin
        a_val = a_mem[a_rd_idx];
        for (rdi = 0; rdi < N; rdi = rdi + 1) begin
            b_vals[rdi] = b_mem[k_idx * N + rdi];
        end
    end

    wire signed [2*DATA_W-1:0] prod [0:N-1];
    wire signed [ACC_W-1:0] prod_ext [0:N-1];

    genvar gj;
    generate
        for (gj = 0; gj < N; gj = gj + 1) begin : gprod
            assign prod[gj] = a_val * b_vals[gj];
            assign prod_ext[gj] = {{(ACC_W-2*DATA_W){prod[gj][2*DATA_W-1]}}, prod[gj]};
        end
    endgenerate

    integer i;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_RECV;
            a_cnt <= 0;
            b_cnt <= 0;
            row_idx <= 0;
            k_idx <= 0;
            col_idx <= 0;
            for (i = 0; i < N; i = i + 1) begin
                acc[i] <= 0;
            end
        end else begin
            case (state)
                S_RECV: begin
                    if (a_handshake) begin
                        for (i = 0; i < LANES; i = i + 1) begin
                            a_mem[a_cnt*LANES + i] <= $signed(in_a_flat[i*DATA_W +: DATA_W]);
                        end
                        a_cnt <= a_cnt + 1;
                    end
                    if (b_handshake) begin
                        for (i = 0; i < LANES; i = i + 1) begin
                            b_mem[b_cnt*LANES + i] <= $signed(in_b_flat[i*DATA_W +: DATA_W]);
                        end
                        b_cnt <= b_cnt + 1;
                    end
                    // transition when both complete (checked on current counts;
                    // takes effect one cycle after last beat, which is fine)
                    if ((a_cnt == A_BEATS) && (b_cnt == B_BEATS)) begin
                        state <= S_COMP;
                        row_idx <= 0;
                        k_idx <= 0;
                        col_idx <= 0;
                    end
                end
                S_COMP: begin
                    // accumulate one k-step for current row across all columns
                    if (k_idx == 0) begin
                        for (i = 0; i < N; i = i + 1) begin
                            acc[i] <= prod_ext[i];
                        end
                    end else begin
                        for (i = 0; i < N; i = i + 1) begin
                            acc[i] <= acc[i] + prod_ext[i];
                        end
                    end
                    if (k_idx == K - 1) begin
                        state <= S_OUT;
                        col_idx <= 0;
                    end else begin
                        k_idx <= k_idx + 1;
                    end
                end
                S_OUT: begin
                    if (out_ready) begin
                        if (col_idx == N - 1) begin
                            // row finished
                            if (row_idx == M - 1) begin
                                // transaction finished
                                state <= S_RECV;
                                a_cnt <= 0;
                                b_cnt <= 0;
                                row_idx <= 0;
                                k_idx <= 0;
                                col_idx <= 0;
                            end else begin
                                row_idx <= row_idx + 1;
                                k_idx <= 0;
                                col_idx <= 0;
                                state <= S_COMP;
                            end
                        end else begin
                            col_idx <= col_idx + 1;
                        end
                    end
                end
                default: begin
                    state <= S_RECV;
                end
            endcase
        end
    end

endmodule
