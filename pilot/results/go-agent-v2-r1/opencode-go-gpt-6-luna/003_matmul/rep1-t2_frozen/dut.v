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
    localparam A_ELEMS = M*K;
    localparam B_ELEMS = K*N;
    localparam OUT_ELEMS = M*N;
    localparam A_BEATS = (A_ELEMS + LANES - 1) / LANES;
    localparam B_BEATS = (B_ELEMS + LANES - 1) / LANES;
    localparam ACW = $clog2(A_BEATS+1);
    localparam BCW = $clog2(B_BEATS+1);
    localparam OW = $clog2(OUT_ELEMS);

    reg [LANES*DATA_W-1:0] a_mem [0:A_BEATS-1];
    reg [LANES*DATA_W-1:0] b_mem [0:B_BEATS-1];
    reg [ACW-1:0] a_count;
    reg [BCW-1:0] b_count;
    reg [OW-1:0] out_count;

    assign in_a_flat_ready = (a_count < A_BEATS);
    assign in_b_flat_ready = (b_count < B_BEATS);
    assign out_valid = (a_count == A_BEATS) && (b_count == B_BEATS);

    always @(posedge clk) begin
        if (!rst_n) begin
            a_count <= 0;
            b_count <= 0;
            out_count <= 0;
        end else begin
            if (in_a_flat_valid && in_a_flat_ready) begin
                a_mem[a_count] <= in_a_flat;
                a_count <= a_count + 1'b1;
            end
            if (in_b_flat_valid && in_b_flat_ready) begin
                b_mem[b_count] <= in_b_flat;
                b_count <= b_count + 1'b1;
            end
            if (out_valid && out_ready) begin
                if (out_count == OUT_ELEMS-1) begin
                    out_count <= 0;
                    a_count <= 0;
                    b_count <= 0;
                end else begin
                    out_count <= out_count + 1'b1;
                end
            end
        end
    end

    reg signed [ACC_W-1:0] sum;
    reg signed [DATA_W-1:0] av;
    reg signed [DATA_W-1:0] bv;
    reg signed [2*DATA_W-1:0] product;
    reg [LANES*DATA_W-1:0] a_beat;
    reg [LANES*DATA_W-1:0] b_beat;
    integer out_row;
    integer out_col;
    integer a_index;
    integer b_index;
    integer kk;
    always @* begin
        out_row = out_count / N;
        out_col = out_count % N;
        sum = 0;
        av = 0;
        bv = 0;
        product = 0;
        a_beat = 0;
        b_beat = 0;
        a_index = 0;
        b_index = 0;
        for (kk = 0; kk < K; kk = kk + 1) begin
            a_index = out_row*K + kk;
            b_index = kk*N + out_col;
            a_beat = a_mem[a_index / LANES];
            b_beat = b_mem[b_index / LANES];
            av = a_beat[(a_index % LANES)*DATA_W +: DATA_W];
            bv = b_beat[(b_index % LANES)*DATA_W +: DATA_W];
            product = av * bv;
            sum = sum + {{(ACC_W-2*DATA_W){product[2*DATA_W-1]}}, product};
        end
    end
    assign out_c = sum;
endmodule
